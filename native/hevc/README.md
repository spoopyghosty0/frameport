# Shared hardware video decoding for Lepton

`frameport_hevc.cpp` implements Android OMX decoders for HEVC Main, H.264
(Constrained) Baseline/Main/(Constrained) High and VP9 Profile 0 using Iris
stateful V4L2 through FFmpeg. Android reports constrained H.264 streams under
separate profile values, so they are listed as stock decoders list them.
These are 8-bit 4:2:0 decoders. Format-aware clients keep Android's existing
codecs for Main10/HDR and protected content; clients selecting by MIME alone
must still respect the component's advertised profiles. Other codec types
retain Android's existing codecs. Applications that explicitly select their
own software decoder bypass this integration. Byte-buffer clients receive full-size
planar frames. Native surface clients receive YUV in Android hardware buffers,
with acquire/release fences. For large hardware-decoded NV12 surfaces (at least
4096×2048 pixels), compatible linear NV12 buffers are imported into Vulkan
and copied on the GPU without CPU pixel access or colour conversion. The decoded
AVFrame remains held until the transfer fence completes, preventing its Iris
capture buffer from being requeued. Explicit FOREIGN queue-family transfers
return both buffers to their Android/decoder owners before delivery. Imports are
cached within a bounded pool and cleared before decoder reconfiguration or flush.
Unsupported layouts, unavailable Vulkan capabilities and import failures retain
the parallel CPU copy; a submitted transfer that fails to complete reports an
OMX error instead of exposing incomplete pixels. This removes the CPU frame copy
on compatible surfaces but still performs one GPU copy; it is not direct decoding
into Android's consumer buffers. Small surfaces use disjoint parallel CPU row bands.
The same persistent worker pool handles native YUV copies, byte-buffer planar
copies and legacy RGBA conversion (helpers start at panorama sizes); all workers finish before the buffer is unlocked or the decoded
frame is released. Legacy software surfaces
retain parallel NEON RGBA conversion. Input timestamps, dynamic dimensions, EOS and
seek/flush are preserved.

This uses the tested Lepton Android 11 SoftOMX ABI. On connection, FramePort
deploys the plugin, codec XML and Podman wrapper once into the agent's shared
`~/.local/share/frameport/video-codec/versions/<manifest-sha256>` store.
Payload checksums are verified before an atomic `current` symlink exposes the
complete version. Older clients cannot downgrade a newer codec revision.
Superseded versions are pruned, keeping the active version and the one it
replaced (a launch that resolved the previous `current` can still mount it).
Existing and new Lepton launchers use the shared wrapper without rebuilding an
APK or selecting a per-game recipe. Only their matching `podman run` gains a
read-only plugin/XML mount and `/dev/video-dec0`. All other Podman operations
pass through. Shared Lepton, drivers and original MP4 assets are unchanged.
Unknown runtime ABIs retain the stock codecs and log why. A future native
Lepton hardware plugin takes precedence.

The wrapper resolves a fallback Podman from PATH outside its own directory
before reading deployment configuration. Lepton's Android-only `env -i PATH`
attach commands use host default paths if their PATH contains no host Podman.
Missing/malformed configuration,
recursive executable paths, mount-preparation errors and failed exec calls
retain the original arguments and launch stock Podman. The agent publishes
`deployment.json` and codec payloads before exposing the complete version.
Merged runtime XML uses separate runtime paths and process-local temporary
filenames, so concurrent app launches do not overwrite each other's staging.

No package check or MP4 scan selects decoding. Older embedded codec assets are
removed when an APK is rebuilt; they are no longer used by the launcher. The
codec has no OpenXR calls, camera poses or composition changes. Batman's
`surface_native` renderer remains package-scoped and separate. Other apps keep
their existing rendering path; hardware decoding alone does not eliminate
surface upload/conversion or GPU rendering bottlenecks.

The plugin enumerates coded formats and probes each at 1920x1080, not Batman's
8K geometry. Unavailable components are omitted so Android retains its stock
decoders. Actual-session admission can still fail (including Iris's concurrent
session-accounting issue); initialization then falls back to FFmpeg software
decoding inside the already-selected component. This preserves the client's
buffer/surface contract without requiring the app to retry another codec.

During hardware playback, bounded references to two GOPs of compressed packets
permit software recovery after a driver-reported damaged picture. Replay drops
already-presented timestamps, retaining ordered output and EOS. The cache is
capped at 64 MiB/512 packets; if no usable keyframe remains, ordinary OMX error
handling applies. After recovery, later seeks flush the software decoder for
that component until reset. This is a fallback, not a repair to the Iris driver. VP9 seek tests
on this Frame exercised it. Capture buffers use FFmpeg's general-purpose budget
of 20 at ordinary sizes, which avoids the stall observed with the earlier
four-buffer H.264 configuration. At panorama sizes the budget follows the
standards' reference-picture bound plus four (10 at 8K HEVC/H.264, 13 for VP9),
instead of holding about 1 GB of 8K capture memory. Only hardware-reported
damage triggers recovery; software decoders conceal damage themselves and their
pictures are output, as Android's stock decoders do.
A seek after the stream has fully drained (replay or loop after EOS) restarts
the open hardware session in place with `V4L2_DEC_CMD_START`, keeping its
buffers; this works for all three codecs. The component checks the restart
result through a read-only FFmpeg option. A failed restart closes that session
and continues in software, rather than waiting indefinitely for hardware
output. A seek during playback reopens the
hardware session: on the tested Iris kernel, stopping and restarting a running
session's queues failed the driver's session admission ("current session not
supported", -12) and left buffers unreturned (videobuf2 kernel warnings), so it
is not used. On the same kernel, reopening VP9 after a seek produced a damaged
capture picture and a kernel fault in `lookup_swap_cgroup_id` during unmap, so
VP9 switches to software on a seek during playback rather than reopening that
hardware session; H.264 and HEVC retain hardware decoding after seeking.
This compatibility guard is independent of app/package identity.

Android CODECCONFIG parameter sets are prepended to the first H.264/HEVC
hardware access unit because FFmpeg's V4L2 wrapper does not submit extradata
to the driver. Parameter sets sent again mid-stream without a flush (streaming
apps, restarted encoders) replace the stored configuration and are submitted
in-band before the next access unit, rather than failing the session. Complete-frame parsers identify keyframes for bounded recovery,
including ACodec clients that omit the OMX input SYNCFRAME flag.

On the tested headset, disabling **hardware video decoding in Steam's interface**
and restarting Steam removed that conflicting session. This setting does not
disable the game's Iris decoder. It is a SteamOS-driver workaround, not a kernel
fix; FramePort does not silently change the global Steam setting.

Signalled bitstream colour (primaries, transfer, matrix, range) is reported to
ACodec as Android's stock decoders do, so the surface dataspace can follow it;
container values take precedence. Legacy RGBA conversion uses the resulting
matrix, BT.709 for unsignalled HD (Android's convention), and full-range BT.601
or BT.709 where signalled. A full-range BT.709 table matches the pinned Arm64
libyuv ABI. Both hardware NV12 and software planar conversions produce RGBA
channel order; native YUV output retains its original planes and dataspace.

Batman's native surface renderer and stereo composition are documented in
[Surface video playback](../../docs/SURFACE_VIDEO.md). The original decoded
panorama is retained; projected eye views are generated on the GPU.

Build on Linux/WSL with Android NDK r27c and the tested Lepton rootfs:

```
python native/hevc/build.py --ndk /path/to/android-ndk-r27c --lepton-root /path/to/Lepton/images/rootfs
```

The build requires Linux x86-64, Python 3.12+, Make, Perl, and a working host C
compiler with libc development headers (FFmpeg builds host tools). The NDK
revision is checked against `27.2.12479018`. Previous FFmpeg objects and install
output are discarded. Both FFmpeg and the plugin use that NDK's compiler and
linker. A private copy of its libc++ headers uses Android's platform `__1`
namespace; `-nostdinc++` prevents mixing these with the NDK's `__ndk1` headers.
The build uses libc++'s verbose-abort customization hook to avoid a newer NDK
abort symbol absent from Android 11's platform runtime. It does not edit the NDK
or ship a second libc++.

Compiler file-prefix maps cover source, NDK and runtime paths. FFmpeg's
generated configuration string is normalized separately because it embeds
literal configure arguments. The manifest records the NDK revision and source
checksum. Two clean builds in different directories produced the same codec
binary SHA256; `.comment` identifies NDK Clang/LLD 18.0.3 throughout. The rebuilt
plugin also passed 600 original 8K frames, a flush/seek, 120 replayed frames and
clean shutdown in an isolated headset container.

AOSP headers in `platform/` come from `android-11.0.0_r48` and retain their
original license notices. `fetch_headers.py` records the upstream paths.
The builder downloads the pinned FFmpeg 7.1.1 archive from
https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz and verifies SHA256
`733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1`.
`build.py` applies explicit wrapper changes: VP9 V4L2 uses the same
`vp9_superframe_split` input filter as the software decoder, and the V4L2
decoder gains a flush callback that restarts a drained decoder in place
(`V4L2_DEC_CMD_START` with both queues still streaming). Its read-only
`frameport_flush_error` option reports failed or premature restarts to the
component, because `avcodec_flush_buffers` itself has no return value.
Without it a restart after EOS could only close and reopen the hardware
session. `v4l2_export.c.inc` adds a capture-buffer export interface inside FFmpeg,
so the component does not access FFmpeg's private buffer structures. The caller
must retain the AVFrame until GPU work completes. Its LGPL build
configuration and those source changes are recorded in the builder. The license
is included in the shared version directory as `COPYING.FFmpeg`.

`omx_decode_probe.cpp` and `media_codec_probe.cpp` exercise decoder selection,
original-size output, timestamps, flush/seek, EOS and teardown on the headset.
The OMX probe's `restartfail` mode uses the fault shim in
`tests/fixtures/src/video_codec_faults.c` to reject a drained-session restart;
`software_rgba` exercises software fallback and legacy surface conversion
together. `color_probe.cpp` compares both platform NEON conversion paths with
the full-range BT.709 equations, including channel order and extreme values.
The OMX probe's `native` mode hashes original-size Android YUV buffers and checks
GPU submissions and replayed pixels. `native_cpu` disables Vulkan initialization;
`native_importfail` rejects memory imports. Both verify fallback preserves the
same surface contract and pixels without GPU submissions.
The native surface fixtures cover buffer ownership, GPU projection and
composition. See the surface documentation for validation and limitations.
