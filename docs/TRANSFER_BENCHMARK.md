# Transfer measurements and incremental installs

## Current uploader

OpenSSH SFTP is the installer backend for files of 8 MiB and more, with Paramiko as the fallback; smaller files go in
tar batches. **Parallel uploads** (Settings → Installing) is 3 by default (1–8; saved preferences are kept). The
sections after "Production OpenSSH follow-up" are the measurements that led there, kept for reference: their
statements about the "production" uploader describe the state at the time.

## Production OpenSSH follow-up

With the OpenSSH backend, the default became three workers. Fresh files use `put`; existing partial files use `reput`. Each worker keeps
one SFTP batch session. Progress is polled through a separate bounded SFTP channel. Native failures resume via
Paramiko; cancellation stops the owned SFTP/SSH process trees and does not trigger fallback.

Windows matched production comparison: eight 64 MiB files, 512 MiB total, three shuffled repetitions per backend,
three workers, same home Wi-Fi/internal storage. OpenSSH median **15.51 MB/s**, Paramiko median **11.08 MB/s**
(about 40% faster). All six checksums passed. OpenSSH trials ranged 9.5–16.8 MB/s, so link variability still
matters; it was slower than Paramiko in one pass. Native credential/progress setup took 0.12–0.13 seconds.

The separate Windows 128 MiB production test also passed every checksum. Native cancellation left a partial file
of 74,752,000 bytes; resuming with OpenSSH produced the expected SHA-256. The log confirmed no fallback for the
resumed production upload.

The earlier sections below preserve measurements made before the production backend switch.

The WSL production validation (128 MiB, three workers, three repetitions) measured 14.1 / 19.1 / 15.0 MB/s,
with every checksum passing and native backend logs recorded. Production cancellation left a 72,330,240-byte
partial file; OpenSSH resumed it in 4.23 seconds with the expected SHA-256 and no Paramiko fallback.

## Measurements (2026-10-14)

Both runs used the same paired Frame, the home-router connection and internal ext4 storage under the paired user's
home. Each large-file trial sent the same 128 MiB random payload; each small-file trial sent 512 files of 64 KiB
(32 MiB total). Methods were shuffled for each of three repetitions. Windows and WSL runs were sequential, not
concurrent. Every completed transfer passed SHA-256 verification. Rates below include connection/setup and transfer
completion, but exclude checksum verification and cleanup; MB/s uses decimal bytes.

| Method | WSL2 median MB/s | Windows 11 native median MB/s |
|---|---:|---:|
| FramePort pipelined SFTP | 10.4 | 6.1 |
| Paramiko SFTP, 64 MiB window | 17.1 | 4.5 |
| OpenSSH SFTP | 16.8 | 15.4 |
| OpenSSH SCP (default protocol) | 11.1 | 10.1 |
| OpenSSH `ssh` + streamed file | 9.9 | 8.1 |
| FramePort tar stream | 14.9 | 6.8 |
| OpenSSH tar stream | 15.0 | 7.2 |

The network varied considerably: WSL's application trials ranged from 9.6–12.8 MB/s, native SFTP from
16.2–17.3 MB/s, and some other methods dropped below 5 MB/s. Windows application trials ranged from 5.6–6.4 MB/s;
native SFTP ranged from 9.1–16.5 MB/s. These are useful end-to-end comparisons on this setup, not a measurement of
the maximum throughput each transport can achieve. Window tuning did not help the Windows run.

Cancellation/resume checks interrupted the application upload at 64 MiB, then independently resumed that partial
file with the application and with OpenSSH SFTP `reput`. All four resumed files passed SHA-256 verification. This
checks application cancellation and both resume paths; native SFTP cancellation itself remains unmeasured.

The Frame's USB and hotspot interfaces were up, but neither direct endpoint was reachable from the PC. No removable
storage was mounted. USB, hotspot and microSD performance remain **unmeasured** in this round. The Frame's router
connection used 5 GHz / 80 MHz Wi-Fi, with roughly -56 dBm signal during a read-only probe. This does not reproduce
the 6 GHz direct-hotspot setup in [discussion #242](https://github.com/spoopyghosty0/frameport/discussions/242), where
the reporter measured about 114 MB/s with OpenSSH versus 23 MB/s with Paramiko.

**Recommendation:** prototype an optional OpenSSH SFTP backend for large files, retaining `.part`/`reput`,
cancellation, retries, strict paired-host verification and Paramiko fallback. Validate it on a fast direct link
before making it the default. On this Windows run SFTP was about 2.5 times faster than the current application,
and it beat SCP. The small-file tar measurements do not justify selecting a new tar transport yet. Production
transport selection remains Paramiko pending that decision. The bounded parallel-file setting described below
uses independent Paramiko connections.

## Parallel-file uploads

### Matched native OpenSSH comparison

The later comparison tested both backends sequentially on Windows and WSL, with three shuffled repetitions of
the same eight 16 MiB files per worker count. Native `sftp-files-N` balances files across N persistent OpenSSH
SFTP batch processes. Setup and completion are included; checksum verification is excluded. All 60 transfers
passed SHA-256 checks. This is benchmark code; the production uploader still uses Paramiko.

| Workers | Windows Paramiko MB/s | Windows OpenSSH MB/s | WSL Paramiko MB/s | WSL OpenSSH MB/s |
|---|---:|---:|---:|---:|
| 1 | 6.22 | 14.96 | 6.17 | 14.64 |
| 2 | 10.61 | 17.73 | 14.41 | 14.45 |
| 3 | 8.90 | 18.89 | 13.65 | 17.75 |
| 4 | 14.90 | 16.80 | 15.67 | 16.63 |
| 8 | 13.50 | 20.16 | 17.03 | 16.82 |

Three is a reasonable default candidate for a future OpenSSH backend: versus two it improved median throughput
by 6.5% on Windows and 22.8% on WSL. Eight was faster on Windows but slower on WSL. The production Paramiko
default remains two because three regressed by 16.1% on Windows and 5.3% on WSL in this matched comparison.
Link variability remains substantial, and no fast hotspot/USB result is claimed.

Settings → Installing → **Parallel uploads** accepts **1–8** (default 2 at the time, 3 now). It controls large files within one
install; the FIFO still runs one install job at a time. A single APK is not split into chunks. Large files are
scheduled largest first, with at most one authenticated SSH connection per active worker and never more workers
than files. Small files still use tar batches after the large-file uploads. Choose 1 on a busy network; a higher
count can consume all available bandwidth and is not automatically faster.

The parallel comparison uses the same eight 16 MiB OBB-shaped files (128 MiB total) for every worker count, with
three shuffled repetitions. The production uploader is used by `app-files-N`, including its aggregate progress,
resume baselines, cancellation checks and connection cleanup. A failed or cancelled cohort stops queued work and
closes only its worker transports; completed files and interrupted `.part` files remain resumable.

| Workers | WSL2 production median MB/s | Windows 11 production median MB/s |
|---|---:|---:|
| 1 | 9.6 | 5.6 |
| 2 (default) | 9.1 | 10.9 |
| 3 | 6.5 | Not measured in the final production run |
| 4 | 12.4 | 14.8 |
| 8 | 11.2 | 17.3 |

On Windows, the production uploader improved from 5.6 to 10.9 MB/s with 2 workers (**1.95 times faster**), and
17.3 MB/s with 8 (**3.08 times faster**). All twelve Windows production transfers passed SHA-256 verification.
The preceding Windows prototype also tested 3 workers (12.2 MB/s median), before aggregate progress/cancellation
were integrated; that is not included as a production measurement in the table.

WSL was noisy: four workers improved its median by about 29%, but 2 did not improve its median, and 3 was slower.
There is no evidence that a particular worker count is universally fastest. The option is retained because it
produced a repeatable benefit on Windows and higher counts improved the WSL median. Default 2 limits competing
transfers while retaining a measured Windows benefit; users can measure higher counts or select 1 in their own
environment. Production code, including cleanup and progress checks, was used for both final comparisons.

```sh
uv run python scripts/upload_bench.py --mib 128 --repeat 3 --methods app-files-1,app-files-2,app-files-3,app-files-4,app-files-8 --json parallel.json
```

`install.upload_threads` persists the setting. Malformed values use the default and numeric values are clamped to
1–8.
The serial setting reuses the existing control connection; parallel workers verify that the new transport reaches
the same paired headset before sending any files.

## Reproducing the measurements

Run from the checkout using the Python environment for the platform being measured:

```sh
uv run python scripts/upload_bench.py --mib 512 --repeat 3 --check-resume --json uploads.json
uv run python scripts/upload_bench.py --fast-link --probe
uv run python scripts/upload_bench.py --fast-link --remote-dir /path/on/card --mib 512 --repeat 3 --json card.json
```

`--host` selects a specific paired endpoint; `--fast-link` uses the same authenticated direct-link discovery as an
install. Inspect the reported endpoint and link before comparing methods. Join the Frame's hotspot or connect its
USB cable before testing those paths; the benchmark does not change PC networking. Keep the headset awake and avoid
other network traffic. A 512 MiB payload is preferred for follow-up tests on fast links; the available-link runs
above used 128 MiB.

The script creates a unique scratch directory on the selected filesystem, checks content outside the timed upload,
and removes its own temporary remote files and temporary key copy. It never modifies the pairing key's ACL. Windows
OpenSSH rejects an `OWNER RIGHTS` ACL entry; the temporary copy grants only the current user, without Python's
Windows `chmod` behaviour that can introduce that entry. Native clients use the application's key and known-hosts
file with strict host verification.

JSON rows contain bytes, total seconds, separately measured setup seconds where available, payload and total rates,
verification duration and checksums. SCP/SFTP's setup is included in total time because their command-line clients
do not expose its completion separately. On POSIX, CPU includes the Python parent and child processes; on Windows,
`os.times()` measures only the parent, so native subprocess CPU numbers must not be compared with Paramiko's CPU.
An error is recorded rather than counted as a successful trial. `--methods` can restrict the run; `--small-files`
controls the tar corpus. Cipher comparisons are available as `ssh-gcm` and `ssh-chacha`.

## Incremental install behaviour

`game_update.plan_update` is the shared, I/O-free Quest/Android classifier used by badges and installation. Recipe
source, notes and catalog revision do not affect it. A newer tested catalog recipe remains a separate suggestion
when the user customized their recipe.

- Build records keep an APK recipe fingerprint, source hash/stat, selected native-asset identities, converter
  identity and the existing validated APK hashes. Reusable local APKs are checksum-checked before installation.
- Deployment records keep applied runtime configuration and its fingerprint, APK stat/hash, source version,
  data manifest/digest and artwork hashes. Routine device samples carry only the digest, and updates fetch a
  detailed manifest only when necessary. Live numeric settings writes update only the applied runtime configuration,
  leaving APK build identity intact. Desired data manifests are cached locally for badges, without render-path I/O.
- Runtime-only updates use the agent's `configure`: no conversion, signing, APK transfer, artwork, Steam restart or
  automatic launch test. They still require the installed APK identity and an available destination, and refuse
  changes while the game is running. Defaults explicitly reset overrides embedded in an older APK. Generated
  configuration files are restored or removed only when they still match FramePort's last write; user edits survive.
- Build support dependencies remain conservative: controller models/native surfaces/haptic support, Zink-layer
  selection, mixed foveation patches and revised adapter support can require a new APK. A recorded alternate
  variant can be reused when switching variants.
- Data-only updates transfer missing/different-size data using the resumable uploader. Recipe/APK updates skip
  data synchronization when the source data manifest is unchanged, preserving Frame-side mods; explicit forced
  reinstalls still check/repair data. Artwork-only
  updates send changed slots; unchanged artwork avoids a Steam refresh. APK/data changes retain the configured
  automatic launch test. Settings/artwork changes skip it.
- APK-only updates preserve installed OBBs, including mods, and do not apply old staged OBBs or artwork from an
  interrupted upload. Ordinary data comparison still uses sizes, matching existing behaviour; equal-size content
  replacement is not detected automatically. Explicit rebuild/reinstall remains available (`install --force`).
- Legacy build identities take the full path once because their original build inputs cannot be established
  safely. Converted-copy cleanup and `build.keep_copies` remain in effect; a configuration/data update does not
  require the deleted converted APK.

Install results record `stage_seconds` in the library's install history. This includes planning, conversion,
validation, transfer and finalization stages reached by that operation; the GUI's subsequent optional launch test
is separate. Use these timings to distinguish build cost, network transfer and device/Steam work when reviewing a
slow install. Configuration success is recorded after writing the configuration and completing its sync; a failed
operation leaves the prior applied fingerprint so it remains retryable.
