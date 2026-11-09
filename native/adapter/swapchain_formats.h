// SPDX-License-Identifier: GPL-3.0-only
// unorm_formats (per game, off by default; GitHub #400): the Frame's runtime lists only sRGB colour formats (GLES
// GL_SRGB8_ALPHA8 / GL_SRGB8, Vulkan R8G8B8A8_SRGB). Apps that look for plain 8-bit RGBA find none and give up on VR
// (flycast: "XR: no RGBA8 swapchain format", then its flat window). With the setting, the 8-bit UNORM twin of each
// listed sRGB format is appended after the runtime's list: the runtime's own formats keep their order, so an app that
// takes the first one still gets sRGB. Creating a swapchain in an appended format fails at the runtime and
// swapchain_fix retries it in the sRGB twin (pick_format), so the images are sRGB: an app that writes gamma-encoded
// colours into them gets a brighter picture.
// Header-only, no Android/GL dependencies: tests/test_framebridge_round2.py compiles it for the host.
#ifndef FRAMEBRIDGE_SWAPCHAIN_FORMATS_H
#define FRAMEBRIDGE_SWAPCHAIN_FORMATS_H
#include <stdint.h>

#define SF_MAX_APPENDED 2

// Appends the UNORM twins (GL_RGBA8 for GLES; VK_FORMAT_R8G8B8A8_UNORM / B8G8R8A8_UNORM for Vulkan) of sRGB formats
// in formats[0..n) that aren't listed themselves. `formats` must have room for n + SF_MAX_APPENDED. Returns the new
// count.
static inline uint32_t sf_append_unorm(int64_t *formats, uint32_t n, int gles) {
    static const int64_t gl_pairs[][2] = {{0x8C43, 0x8058}};       // GL_SRGB8_ALPHA8 -> GL_RGBA8
    static const int64_t vk_pairs[][2] = {{43, 37}, {50, 44}};     // R8G8B8A8_SRGB -> _UNORM, B8G8R8A8_SRGB -> _UNORM
    const int64_t (*pairs)[2] = gles ? gl_pairs : vk_pairs;
    unsigned pair_count = gles ? 1 : 2;
    uint32_t total = n;
    for (unsigned p = 0; p < pair_count; ++p) {
        int has_srgb = 0, has_unorm = 0;
        for (uint32_t i = 0; i < total; ++i) {
            has_srgb |= formats[i] == pairs[p][0];
            has_unorm |= formats[i] == pairs[p][1];
        }
        if (has_srgb && !has_unorm) formats[total++] = pairs[p][1];
    }
    return total;
}
#endif
