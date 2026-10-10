// SPDX-License-Identifier: GPL-3.0-only
// Arm64 table ABI of the pinned Android 11 libyuv (row.h / row_neon64.cc):
// https://android.googlesource.com/platform/external/libyuv/+/android-11.0.0_r48/files/include/libyuv/row.h
#ifndef FRAMEPORT_YUV_COLOR_H
#define FRAMEPORT_YUV_COLOR_H
#include <stdint.h>
struct alignas(16) YuvConstants {
    uint16_t rb[8],rb2[8],g[8],g2[8];
    int16_t bias[8];
    int32_t y[4];
};
static_assert(sizeof(YuvConstants)==96,"pinned Arm64 libyuv table ABI");
// Full-range BT.709: R=Y+1.5748(V-128), B=Y+1.8556(U-128),
// G=Y-0.187324(U-128)-0.468124(V-128). Libyuv uses six fractional
// bits and a 32 rounding bias. Mirrored coefficients + swapped U/V
// produce RGBA bytes with its ARGB row functions, as in the NV12 path.
static constexpr YuvConstants frameport_full709_rgba={
    {101,119,101,119,101,119,101,119},
    {101,119,101,119,101,119,101,119},
    {30,12,30,12,30,12,30,12},
    {30,12,30,12,30,12,30,12},
    {-12896,5408,-15200,0,0,0,0,0},
    {0x0101*16320,0,0,0}};
#endif
