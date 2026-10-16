#!/bin/bash
# Reproduce the exact Wine source of a GE-Proton release (Wine submodule + wine-staging + GE's own patches): the WINE
# section of GE's patches/protonprep-valve-staging.sh, without the DXVK/vkd3d/protonfixes steps.
# usage: prep_ge_wine.sh <work dir> [tag=GE-Proton11-7]   → <work dir>/src/wine
# Needs git, patch, python3, autoconf/autoreconf (the WINE section ends with autoreconf + tools/make_requests).
set -e
W=$1; TAG=${2:-GE-Proton11-7}
[ -n "$W" ] || { echo "usage: $0 <work dir> [tag]"; exit 2; }
mkdir -p "$W" && cd "$W"
[ -d src ] || git clone -q --depth 1 --branch "$TAG" https://github.com/GloriousEggroll/proton-ge-custom src
cd src
git submodule update --init --depth 1 wine wine-staging
P=patches/protonprep-valve-staging.sh
start=$(grep -n "^### (2) WINE PATCHING ###" $P | cut -d: -f1)
end=$(grep -n "^### END WINE PATCHING ###" $P | cut -d: -f1)
fn_end=$(grep -n "^### (1) PREP SECTION ###" $P | cut -d: -f1)
[ -n "$start" ] && [ -n "$end" ] && [ -n "$fn_end" ] || { echo "unexpected layout of $P"; exit 1; }
# the WINE section reverts commits from the Wine history: fetch them into the shallow clone first
for c in $(sed -n "${start},${end}p" $P | grep -oE "git revert [^#]*" | grep -oE "[0-9a-f]{40}"); do
  git -C wine fetch -q --depth 2 origin "$c"
done
{ sed -n "1,$((fn_end - 1))p" $P; sed -n "${start},${end}p" $P; } > ../wine_only.sh
bash ../wine_only.sh > ../prep.log 2>&1
grep -n "SERVER_PROTOCOL_VERSION" wine/include/wine/server_protocol.h
if find wine -name "*.rej" | grep -q .; then echo "rejected hunks, see ../prep.log"; exit 1; fi
echo "patched Wine tree: $W/src/wine"
