#!/bin/bash
# Build the four patched Wine PE DLLs FramePort adds to GE-Proton for the Meta runtime (see README.md), from GE's
# exact Wine source (prep_ge_wine.sh) + patches/*.patch, on an x86_64 Linux host with llvm-mingw on PATH.
# Only these DLLs are built: generated headers they don't use (Vulkan thunks) are empty stand-ins, the syscall
# tables come from Wine's own tools/make_specfiles.
# usage: build_pe_dlls.sh <patched GE wine dir (prep_ge_wine.sh)> <work dir> <out dir>
set -e
SRC=$(cd "$1" && pwd); W=$2; OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
[ -n "$OUT" ] || { echo "usage: $0 <wine dir> <work dir> <out dir>"; exit 2; }
command -v x86_64-w64-mingw32-clang >/dev/null || { echo "llvm-mingw (x86_64-w64-mingw32-clang) must be on PATH"; exit 2; }
mkdir -p "$W" "$OUT"; W=$(cd "$W" && pwd); OUT=$(cd "$OUT" && pwd)
if [ ! -d "$W/wine" ]; then
  cp -a "$SRC" "$W/wine"
  for p in "$HERE"/patches/*.patch; do patch -d "$W/wine" -p1 -s < "$p"; echo "applied $(basename "$p")"; done
  (cd "$W/wine" && perl tools/make_specfiles >/dev/null)
  for f in include/wine/vulkan.h dlls/winevulkan/loader_thunks.c dlls/winevulkan/loader_thunks.h \
           dlls/winevulkan/vulkan_thunks.c dlls/winevulkan/vulkan_thunks.h dlls/winevulkan/winevulkan.json; do
    [ -e "$W/wine/$f" ] || : > "$W/wine/$f"
  done
fi
mkdir -p "$W/obj"
if [ ! -f "$W/obj/Makefile" ]; then
  # no debug info and relative source paths: the DLLs are committed (no local paths may end up in them)
  (cd "$W/obj" && CROSSCFLAGS="-O2 -ffile-prefix-map=$W/wine=wine -ffile-prefix-map=$W/obj=obj" \
     "$W/wine/configure" --enable-archs=x86_64 --disable-tests --without-x --without-freetype \
     --without-wayland --without-gstreamer --without-pulse --without-alsa --without-oss --without-cups --without-dbus \
     --without-gnutls --without-krb5 --without-netapi --without-opencl --without-pcap --without-sane --without-udev \
     --without-usb --without-v4l2 --without-vulkan --without-sdl --without-capi --without-gphoto --without-fontconfig \
     --without-coreaudio --without-opengl --without-inotify --without-unwind --without-ffmpeg > configure.log 2>&1)
fi
DLLS="crypt32 sechost dnsapi windows.devices.enumeration"
cd "$W/obj"
make -j"$(nproc)" $(for d in $DLLS; do echo "dlls/$d/x86_64-windows/$d.dll"; done) > make.log 2>&1 || { tail -20 make.log; exit 1; }
for d in $DLLS; do cp "dlls/$d/x86_64-windows/$d.dll" "$OUT/"; done
# Wi-Fi WinRT stand-in (not a Wine DLL: goes into the prefix's system32 + its activation class registry key)
x86_64-w64-mingw32-clang -O2 -shared -s -Wl,--no-insert-timestamp -o "$OUT/windows.devices.wifi.dll" \
  "$HERE/wifi_stub.c" "$HERE/wifi_stub.def" -lole32 -lwindowsapp
ls -la "$OUT"
