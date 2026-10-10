# Third-party notices

FramePort is GPL-3.0-only (see `LICENSE`). It includes or downloads the following third-party work.

## Included in this repository / the release bundles
| Component | Where | License |
|---|---|---|
| OVRPort VrApi → OpenXR adapter (`native/vrapi`, github.com/Android-XR-Bridge/OVRPort), with FramePort's changes | `native/vrapi-bridge/`, `artifacts/arm64-v8a/libvrapi.so` | GPL-3.0 (`native/vrapi-bridge/LICENSE.upstream`) |
| OVRPort platform compatibility library (`native/platform`) | `native/platformcompat/`, `artifacts/arm64-v8a/libovrplatformcompat.so` | GPL-3.0; Meta Platform SDK headers under the Meta Platform Technologies SDK License (`native/platformcompat/licenses/OCULUS-PLATFORM-SDK.txt`) |
| Khronos OpenXR SDK headers | used to build the native layers | Apache-2.0 / MIT (`native/vrapi-bridge/licenses/OPENXR-SDK.txt`) |
| Android NDK runtime (statically linked libc++) | native Android libraries | Apache-2.0 with LLVM exception, plus legacy notices (`native/vrapi-bridge/licenses/ANDROID-NDK.txt`) |
| Flet and Flutter (desktop app runtime) | release bundles | Apache-2.0 / BSD-3-Clause |
| flet-dropzone / desktop_drop (drag-and-drop) | release bundles | Apache-2.0 / MIT |
| FFmpeg 7.1.1 hardware HEVC wrapper (LGPL configuration, without GPL/nonfree components) | `artifacts/hevc/libstagefrighthw.so`; source/rebuild instructions in `native/hevc/build.py` and `native/hevc/README.md` | LGPL-2.1-or-later (`artifacts/hevc/COPYING.FFmpeg`); unmodified source: https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz |
| AOSP Android 11 native media/utility headers | `native/hevc/platform/` | Apache-2.0; copyright/license notices retained in the headers |
| Python packages (paramiko, zeroconf, psutil, pyelftools, capstone, UnityPy, PyYAML, requests, typer, pyaxmlparser, Pillow, cryptography, …) | release bundles | their own licenses (see each package's metadata) |

FramePort's own native code (the FrameBridge adapter, GL/Vulkan/OpenXR shims, the Windows helpers) is GPL-3.0-only.

## Downloaded at run time (not redistributed)
| Tool | Source | License |
|---|---|---|
| OVRPort / overport CLI | github.com/Android-XR-Bridge/OVRPort releases (fallback github.com/ovrport/app) | GPL-3.0 |
| Eclipse Temurin JRE | api.adoptium.net | GPL-2.0 with Classpath Exception |
| apksigner (Android build-tools) | dl.google.com Android repository | Apache-2.0 |
| Revive (portable copy, only if Revive isn't installed) | github.com/LibreVR/Revive releases | GPL-3.0 |

Valve's Lepton, Proton and SteamVR, and Meta's software, are used on your devices as installed by Steam / Meta;
FramePort doesn't distribute them.
