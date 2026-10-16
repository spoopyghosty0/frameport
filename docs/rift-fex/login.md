# Stream A: Meta sign-in on the Frame

Implemented on `rift/login`. No Frame or network access was used. The PE helper has **not** been compiled here
(llvm-mingw is unavailable); the browser portal and pipe framing remain device acceptance checks.

## What was built

`frameport fex login [--start-now] [--timeout 900] [--via-pc]` signs in through Meta's own Client.exe, running in the
existing shared FEX prefix. The default creates a temporary, flat Steam entry named **Meta sign-in (FramePort)**
using the existing shortcut installer. `--start-now` instead starts a detached user unit without restarting Steam.
`fex login --stop` repairs interrupted cleanup. All commands require `FRAMEPORT_EXPERIMENTAL_FEX=1`.

`agent/fexrift_login.py` registers `login_start`, `login_status`, `login_stop` and private broker/callback commands.
`agent/frameport_agent.py` has one separate `fex_login*` block; it passes login messages to the extension over stdin,
never JSON argv. No agent version bump or changes to other streams' hook points were needed.

The launch script imports Steam's display variables, disables gamescope WSI and preloads, starts the existing Meta
runtime, waits for OVRLibrarian's completion and kills its stuck process, then starts Client.exe with Proton. Its
stdout/stderr and Proton logging are disabled because Electron can print authentication URLs. A lock prevents a
second Client.exe launcher. A private broker must be alive before the launcher can run. Cancellation signals the
launcher (PID plus kernel start tick, to reject a reused PID), stops Wine, then restores the handler settings. This
also prevents an interrupted runtime warm-up from starting Client.exe after cleanup.

The prefix's WineBrowser `Browsers` setting temporarily points to `login/fp-open-url`. That helper sends the URL to
an owner-only Unix socket broker. The broker records only `page_opened_at`, `callback_received_at` and the fixed
query-free page URL in `login/state.json`. It opens the real URL with host xdg-open, with output discarded. The
temporary host `frameport-oculus.desktop` invokes `fp-oculus-callback`, which sends the callback to the same broker.
The broker passes it to `proton run fp_pipesend.exe` **through stdin**. Neither agent forwarding nor Wine invocation
puts the callback into argv. The desktop protocol invocation itself necessarily supplies `%u` to its short-lived
handler; do not collect process-command-line diagnostics during sign-in.

`native/fexwine/pipesend.c` sends a NUL-terminated UTF-8 callback to `\\.\pipe\oculus`. It retries a missing or busy pipe
for about 20 seconds and prints nothing. Exit codes: 0 written, 2 invalid input, 3 pipe unavailable, 4 write failed.
Only Meta's running client receives the link; a second Client.exe is never started to deliver it.

The extension snapshots the previous WineBrowser value/type, previous host scheme owner, desktop file and mimeapps
files before registration. `login/restore.json` contains **registration settings only**. A failed restoration leaves
this record for `--stop` to retry. Normal success, timeout, errors and Ctrl-C all clean up; a broker deadline also
restores the Frame settings if the PC disconnects (requested timeout plus 180 seconds for Steam shortcut setup).
An OS kill/reboot can interrupt this cleanup; rerun `--stop` after reconnecting. The temporary shortcut is removed
by the PC's cancel request, asynchronously through the existing shortcut worker. A disconnected PC cannot remove
that shortcut until it reconnects, but the launcher refuses to run without its broker.

## PC browser fallback

`--via-pc` keeps the full auth URL **only in broker memory**, draining it once through the agent's SSH status reply
when the PC explicitly requests `take_url`. Ordinary `fex_login_status` replies never contain the URL. The PC opens
the page in its default browser and temporarily borrows the `oculus` scheme. The handler POSTs directly to a loopback
server with a random endpoint; it never uses the normal install-link `links/*.link` inbox. HTTP logging is disabled.
WSL registers a Windows handler which invokes the WSL Python callback, and opens the sign-in page in Windows.

Windows/WSL restoration exports and restores the **entire** HKCU `Software\Classes\oculus` tree, including arbitrary
subkeys and value types, or removes it if absent before login. The non-secret export is deleted after restoration;
if importing it fails, a recovery copy is retained as `<FRAMEPORT_HOME>/oculus-handler-restore.reg` and the error
names it. Linux restores the exact previous desktop/mimeapps bytes, including an absent default. macOS fallback
is explicitly unsupported. These temporary registrations are separate from the permanent FramePort install-link
settings. The PC context restores registration even when sign-in, SSH forwarding or Frame cleanup raises.

Saved login state in `sessions/` and CoreData stays in the prefix. Status only checks whether a non-empty session
file exists; it does not read its contents, prove the session is unexpired, or imply entitlement to any game. This
change does not replace Platform SDK DLLs, alter entitlement checks, or access repack archives. A game reporting
**Missing entitlement** must remain an entitlement failure.

## Offline verification

Run from this worktree, keeping app data isolated in its existing `FRAMEPORT_HOME`:

```sh
UV_CACHE_DIR=/tmp/frameport-uv-cache uv run --offline pytest -q tests/test_fex_login.py tests/test_fexrift.py tests/test_fexrift_ext.py
UV_CACHE_DIR=/tmp/frameport-uv-cache uv run --offline ruff check agent/fexrift_login.py src/frameport/fexlogin.py src/frameport/urlhandler.py src/frameport/cli.py tests/test_fex_login.py
UV_CACHE_DIR=/tmp/frameport-uv-cache uv run --offline pytest -q
```

The focused suite passed (56 passed, 1 skipped); lint passed. The full suite rerun completed: **1,651 passed, 43 skipped,
11 failed, 16 errors**. Every failure/error was `PermissionError: [Errno 1] Operation not permitted` from existing
local HTTP/pairing/live-stream socket tests: this sandbox prohibits socket creation/binding. No other stream's files
or tests were changed to hide those failures. Run the whole suite on a host permitting loopback sockets before
integration. No triage/patch definitions were changed, so patch-doc regeneration is not required.

## Exact maintainer device acceptance steps

Use the maintainer's paired Frame configuration; do not copy credentials or configuration into this branch.
Close any Rift game first: sign-in uses the same Wine prefix and stopping it ends that prefix's processes.

1. Build the PE with llvm-mingw on an x86_64 host (or run the existing full `build_pe_dlls.sh` build):

   ```sh
   x86_64-w64-mingw32-clang -O2 -s -Wl,--no-insert-timestamp -o artifacts/fexrift/fp_pipesend.exe native/fexwine/pipesend.c
   uv run python - <<'PY'
   import hashlib
   from pathlib import Path
   root = Path("artifacts/fexrift")
   sums = root / "SHA256SUMS"
   lines = [s for s in sums.read_text().splitlines() if s.strip() and s.split()[-1].lstrip("*") != "fp_pipesend.exe"]
   lines.append(hashlib.sha256((root / "fp_pipesend.exe").read_bytes()).hexdigest() + "  fp_pipesend.exe")
   sums.write_text("\n".join(lines) + "\n")
   PY
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex setup
   ```

   Setup uploads the extension automatically alongside the existing FEX artifacts. Without the exe, login must fail
   with `fp_pipesend.exe missing: build native/fexwine/pipesend.c and upload it with fex setup`.

2. Record the Frame's current scheme owner (substitute its paired SSH destination for `FRAME`):

   ```sh
   ssh steamos@FRAME 'xdg-mime query default x-scheme-handler/oculus'
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex login --timeout 900
   ```

   Expect `Put on the headset: open 'Meta sign-in (FramePort)' in your Steam library.` Open that entry. Allow about
   two minutes for the runtime warm-up. Confirm Meta's app is a flat window, not another VR game.

3. Choose Meta's sign-in action. Confirm Chromium opens **on the Frame**. The PC should print `Sign-in page opened
   on the Frame.` Complete the real Meta sign-in. When Chromium asks to open an external app, accept it. This is the
   unproven Flatpak boundary: OpenURI through xdg-desktop-portal must resolve the host's temporary `oculus` handler.
   Expect `Sign-in response reached Meta's app on the Frame.` followed by `Signed in.` The CLI closes the app/runtime,
   restores handlers and requests removal of the Steam entry. Check the Steam entry disappears after the shortcut
   worker finishes; Steam may restart. The saved login must remain visible as `"login": true` in:

   ```sh
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex status
   ssh steamos@FRAME 'xdg-mime query default x-scheme-handler/oculus'
   ```

   The second command must return the original owner (including no owner, if originally unset). Do not dump Meta
   logs, sessions, CoreData, process argv or the full browser URL for debugging.

4. On an unsigned-in test prefix, or before completing the first sign-in, test the alternate launch route:

   ```sh
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex login --start-now --timeout 900
   ```

   Expect `Meta's app is starting on the Frame.` There should be no Steam restart and no new shortcut. After a
   successful login already exists, this command can immediately report `Signed in.` and clean up; that does not
   exercise a fresh browser exchange. Do not delete the owner's session files just to force this test.

5. If the portal never delivers the callback, cancel with Ctrl-C and use:

   ```sh
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex login --start-now --via-pc --timeout 900
   ```

   Click sign-in in the Frame's Meta window; the page must open in the PC browser. Expect `Sign-in page opened in
   the PC browser.`, then the response/success messages above. On Windows/WSL, verify Meta's previous
   `HKCU\Software\Classes\oculus` registration is restored; on Linux verify the original xdg default. This also
   needs a real Windows default-protocol check: OS UserChoice policy can override a Classes registration.

6. To inspect progress without exposing URLs, use the ordinary agent status command from a second terminal:

   ```sh
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run python - <<'PY'
   import json
   from frameport.cli import _fex_target
   frame = _fex_target(None).frame
   print(json.dumps(frame.agent("fex_login_status"), indent=2))
   PY
   ```

   Expected keys: `runtime_running`, `client_running`, `page_opened_at`, `callback_received_at`, `signed_in`. Runtime
   should become true before client; timestamps should appear in that order; signed_in should become true only
   after a non-empty session file exists. If callback_received_at appears but signed_in never becomes true on a
   fresh sign-in, the **pipe framing remains unverified**: this implementation uses UTF-8 plus a terminating NUL.
   Compare against the maintainer's successful spike sender's bytes without printing the URL. Do not retry by
   starting a second Client.exe, altering entitlement checks, or exposing tokens in diagnostics.

7. Test cancellation during runtime warm-up, timeout with `--timeout 10`, and repeatable repair/undo:

   ```sh
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex login --stop
   FRAMEPORT_EXPERIMENTAL_FEX=1 uv run frameport fex login --stop
   ```

   Expect `Meta sign-in stopped. Your saved login stays on the Frame.` both times. No delayed Client.exe should
   appear after waiting two minutes. Status should show client/runtime false, the previous protocol owner should
   be restored, and the session files should remain. If SSH was lost, the broker cleans up the Frame by its deadline;
   reconnect and run `--stop` to remove a leftover shortcut or retry an interrupted restoration.
