// Test fixture for patches/frame/slz_vulkan_hooks.py: the two registrations of Stress Level Zero's graphics plugin
// (same instruction pattern as BONELAB 1.2974's libSLZQuestNative.so).
// Build: aarch64-linux-android29-clang -shared -fPIC -nostdlib fakeslz.S -o ../libfakeslz_arm64.so
    .section .rodata
sampler_name:
    .asciz "vkCreateSampler"

    .text
    .globl RenderAPI_Vulkan_OnPluginLoad
    .type RenderAPI_Vulkan_OnPluginLoad, %function
RenderAPI_Vulkan_OnPluginLoad:
    ldr x8, [x0]
    blr x8                      // GetInterface(IUnityGraphicsVulkanV2): stays
    ldr x8, [x0, #0x70]
    adrp x0, intercept
    add x0, x0, :lo12:intercept
    mov x1, xzr
    mov w2, wzr
    blr x8                      // AddInterceptInitialization(intercept, NULL, 0): switched off
    ret
    .size RenderAPI_Vulkan_OnPluginLoad, .-RenderAPI_Vulkan_OnPluginLoad

intercept:
    ret

    .globl _ZN8PluginVk12GfxEventInitEv
    .type _ZN8PluginVk12GfxEventInitEv, %function
_ZN8PluginVk12GfxEventInitEv:
    ldr x8, [x0]
    blr x8                      // another call: stays
    adrp x0, sampler_name
    adrp x1, intercept
    add x0, x0, :lo12:sampler_name
    add x1, x1, :lo12:intercept
    ldr x8, [x9, #0x8]
    blr x8                      // InterceptVulkanAPI("vkCreateSampler", hook): switched off
    ret
    .size _ZN8PluginVk12GfxEventInitEv, .-_ZN8PluginVk12GfxEventInitEv
