# Batman cutscene playback on Steam Frame

Batman: Arkham Shadow (`com.camouflaj.manta`) submits its prerecorded cutscenes
through Android video surfaces and stereoscopic 180-degree OpenXR equirectangular
layers. Lepton/SteamVR's missing surface/layer support leaves those layers black,
while the separately played audio continues. Uploading software-decoded panorama
pixels also does not provide practical playback of the original 8192x4096 HEVC
assets.

## Scope and installation

FrameBridge enables the internal native-video path only for this package's
arm64 APK. Hardware decoding is deployed independently into FramePort's shared,
versioned codec store and automatically exposed to compatible Lepton app
containers, without embedding decoder assets or requiring per-game recipes.
The launcher places the shared wrapper on its child PATH. Matching containers
receive read-only plugin/XML mounts and the Iris decoder device. Shared Lepton
files and original MP4/OBB assets are
never replaced, transcoded, resized, or rewritten.

The native projection, view recording and Vulkan-enable hooks are gated by
`surface_native`. With it off, Vulkan function lookup and directly exported
entry points pass through to the original loader; default `surface_emul`
alone does not enable the new Vulkan projection path. The fixture
`tests/fixtures/src/surface_hooks_test.c` checks function identity with the
setting off/on and direct-export forwarding with it off. Compile it with
`FRAMEPORT_FAKE_RUNTIME` as `libopenxr_loader_original.so`, then without that
define as the client; run each setting in a separate process next to the
production adapter and fake loader.

The wrapper verifies the tested Android 11 SoftOMX ABI. An existing runtime
hardware plugin takes precedence; incompatible runtimes keep their stock codecs.
Hardware decoder capacity is probed before the private component is advertised.
If the driver's session limit is exhausted, Android can fall back to software
decoding; that preserves functionality but not full-resolution performance.
FramePort does not change Steam's global hardware-decoding settings.

Other packages and arm32 APKs do not receive the native-video setting. The
decoder is separate from that setting, and runs in Lepton's arm64 media service.
Rebuilding an older APK removes the previously bundled codec files; launcher
migration replaces its old wrapper with the common version.
MP4 presence alone is not evidence of compatible surface/overlay semantics.

Package-specific patch revisions use the existing Update on Frame state without
adding UI controls or wording. The shared adapter revision is unchanged; only
Batman is marked outdated for this repair. Older builds without recorded recipe
fingerprints are handled with the same package scope.

## Decode and render path

`native/hevc/frameport_hevc.cpp` exposes `OMX.frameport.hevc.decoder` through the
tested SoftOMX ABI, using FFmpeg's LGPL Iris V4L2 hardware decoder wrapper.
Native surface clients receive full-resolution YUV hardware buffers with fences.
Compatible large native surfaces use fenced Vulkan transfers between imported
decoder and Android DMA buffers, retaining the decoded frame until GPU completion.
Other surfaces retain parallel NV12 row copies without CPU color conversion.
Input timestamps, EOS, dynamic dimensions, seek and flush
are preserved.

An AImageReader retains the decoder's actual image. The surface worker samples
that original YUV buffer and fuses conversion with visible-view projection on
the GPU. Vulkan imports the finished shared view with explicit foreign ownership
transfers. There is no full-panorama RGB intermediate or CPU picture readback.
The default native output is 2560x2560 per eye with a 12% tangent-space guard;
an explicit `equirect_res` takes precedence.

Every finished view carries the camera that actually generated its pixels.
Submitted game projection views are preferred over another tracking query.
The worker coalesces video/view arrivals and reuses held pixels only while the
visible frustum remains covered. Color/source changes redraw content without
unnecessarily changing the covered panorama-local sampling camera. Infinite
panoramas retain their layer rotation and have no translational parallax.

## Stable composition

A separately submitted projected video layer can alternate with the main game
projection as opaque coverage changes during head movement. That presentation
path exhibited snap-back/doubled video in headset tests even when rendering
and submitted camera metadata agreed.

The native path instead snapshots the game's successfully waited stereo color
image **before forwarding its release**, restoring its color-attachment layout
after the copy. Acquisition indices are tracked in FIFO order. The application
image is never sampled or modified after its OpenXR release, and release is not
deferred. Snapshot generations must match the last successful release.

The final GPU pass reprojects the cached video into the current game camera,
blends it with the scene in linear light, and draws directly into a runtime sRGB
attachment. One primary projection contains both scene and video, including
fades and uncovered hemisphere edges. Later subtitles/menu layers keep their
order. Once this output is ready, movie updates copy only to its private cache,
avoiding the additional standalone-output copy.

Batman attaches color-scale/bias and image-layout structures even when their
transforms are identity. The compositor applies RGB/alpha scale and bias using
OpenXR's straight-color transform and restored premultiplication, and respects
vertical orientation. Unknown scene/view chains, unsupported layout flags, and
intervening layers keep the ordinary fallback; settings are not silently lost.
The push constants fit Vulkan's guaranteed 128-byte minimum.

Each output image has separate commands, descriptors and fences. Positive wait
timeouts retain acquisition for retry. Missing snapshots retain the previous
combined output with the exact camera that produced it; old pixels are never
relabelled with a newer pose. Teardown retires snapshot writers and compositor
readers. Depth/motion-vector images from another render are not attached.

## Building and validation

Rebuild FrameBridge with Android NDK r27c:

```sh
python native/build.py --only adapter --ndk /path/to/android-ndk-r27c
```

The hardware plugin builder and its pinned source/runtime requirements are in
`native/hevc/README.md`. Shader sources and their generated SPIR-V are included.
Compile them for Vulkan 1.0 and validate with `spirv-val` when changing them.

The host tests cover package-scoped settings/assets/freshness, existing-wrapper
updates, codec extraction checksums, runtime/container isolation, and preservation
of original video assets. Android fixtures in `tests/fixtures/src/` exercise the
production Vulkan shaders, hardware worker, camera/coverage helpers and YUV copies.
The original-8K worker test requires a lawfully supplied cutscene file and the
private codec in an isolated Android container; no game assets are distributed.

Validation on Steam Frame included stereo/fade/transparent/uncovered pixels,
asymmetric vertical orientation, color/alpha transforms, unsupported-chain
fallback, timeout retry, held camera metadata, reader teardown, hardware playback,
pause/resume and seek. One isolated run presented 594/600 and 118/120 distinct
scheduled original pictures, none early; 120 moving-head composition frames
averaged 1.16 ms for submission plus GPU completion. These are isolated fixture
results, not a guarantee of full-game frame rate.

The final build was also tested in Batman in-headset: cutscenes displayed and
head-movement snap-back was reported resolved. Logs confirmed the combined path,
full-resolution hardware decoding, over 12,500 scene frames with only two startup
holds, and no subsequent old promotion messages or composition setup failures.
Some frame-rate dips and existing Unity render-pass warnings remain. The patch
does not claim universal game compatibility, exact Quest 3 pixel equivalence,
or perfect frame timing.
