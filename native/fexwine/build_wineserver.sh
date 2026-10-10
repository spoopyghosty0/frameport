#!/bin/bash
# Build a native aarch64 wineserver from a patched GE Wine tree (prep_ge_wine.sh), for x86_64 Wine under FEX.
# Run it ON an aarch64 Linux host (e.g. the Frame: gcc, make, flex, bison, autoconf are in SteamOS).
#  - Wine's configure insists on a PE cross compiler for aarch64 and on generated headers this tree lacks; the server
#    needs neither, so configure only produces include/config.h here and the server sources are compiled directly.
#  - -DFP_EMULATED_X86_64: the server takes the x86_64 branch of init_supported_machines (native machine AMD64), as
#    the clients are x86_64 processes under FEX; otherwise it would advertise ARM64 and refuse/misplace x86_64 images.
# The result talks the same protocol as GE's x86_64 wineserver (check: SERVER_PROTOCOL_VERSION and the request list).
# usage: build_wineserver.sh <patched wine dir> <build dir>   → <build dir>/ws/wineserver
set -e
SRC=$(cd "$1" && pwd); B=$2
[ -n "$B" ] || { echo "usage: $0 <wine dir> <build dir>"; exit 2; }
[ "$(uname -m)" = aarch64 ] || { echo "run this on aarch64"; exit 2; }
mkdir -p "$B/obj" "$B/ws"; B=$(cd "$B" && pwd)
grep -q FP_EMULATED_X86_64 "$SRC/server/registry.c" || \
  sed -i 's/^#elif defined(__x86_64__)$/#elif defined(__x86_64__) || (defined(__aarch64__) \&\& defined(FP_EMULATED_X86_64))/' "$SRC/server/registry.c"
grep -q FP_EMULATED_X86_64 "$SRC/server/registry.c" || { echo "registry.c layout changed"; exit 1; }
if [ ! -f "$B/obj/include/config.h" ]; then
  cp "$SRC/configure" "$B/configure.fp"
  sed -i 's/as_fn_error $? "PE cross-compilation is required/echo "(ignored) PE cross-compilation is required/' "$B/configure.fp"
  # configure.fp must run from the source dir's point of view: copy it in, run, remove
  cp "$B/configure.fp" "$SRC/configure.fp"
  (cd "$B/obj" && "$SRC/configure.fp" --srcdir="$SRC" --without-mingw --without-x --without-wayland --without-freetype \
     --without-gstreamer --without-vulkan --without-opengl --without-pulse --without-alsa --without-sdl --without-udev \
     --without-usb --without-gnutls --without-cups --without-dbus --without-krb5 --without-netapi --without-pcap \
     --without-sane --without-v4l2 --without-gphoto --without-unwind --without-ffmpeg --disable-tests > configure.log 2>&1 || true)
  rm -f "$SRC/configure.fp"
  [ -f "$B/obj/include/config.h" ] || { echo "configure did not produce include/config.h (see $B/obj/configure.log)"; exit 1; }
fi
cd "$B/ws"
CF="-c -O2 -g -pipe -fno-strict-aliasing -D__WINESRC__ -D_GNU_SOURCE -DFP_EMULATED_X86_64 -I. -I$B/obj/include -I$SRC/include -I$SRC/server"
for s in $(grep -oE "^\s+[a-z_0-9]+\.c" "$SRC/server/Makefile.in" | tr -d "\t "); do
  extra=()
  case $s in
    unicode.c) extra=('-DBINDIR="/usr/bin"' '-DDATADIR="/usr/share"') ;;   # nls dir = <bin>/../share/wine/nls
    user.c) extra=(-include stdarg.h) ;;
  esac
  gcc $CF "${extra[@]}" -o "${s%.c}.o" "$SRC/server/$s"
done
gcc -o wineserver ./*.o -lrt -lm
echo "built $B/ws/wineserver (protocol $(grep -oE "SERVER_PROTOCOL_VERSION [0-9]+" "$SRC/include/wine/server_protocol.h" | awk '{print $2}'))"
