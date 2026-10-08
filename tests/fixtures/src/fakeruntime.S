// Test fixture for patches/upstream.py probes: the two OVRPort runtime functions ovrport/app#73 fixed, as runtime
// 3.4.3-23204ea has them (default) and as 3.4.3-aa54c3f does (-DFIXED); same instruction sequences as those builds.
// Build: aarch64-linux-android29-clang -shared -fPIC -nostdlib fakeruntime.S -o ../libfakeruntime_old_arm64.so
//        aarch64-linux-android29-clang -shared -fPIC -nostdlib -DFIXED fakeruntime.S -o ../libfakeruntime_fixed_arm64.so
    .text
    .globl AAudioStream_getFramesPerBurst
    .type AAudioStream_getFramesPerBurst, %function
AAudioStream_getFramesPerBurst:
    ldr w0, [x0]
    ret
    .size AAudioStream_getFramesPerBurst, .-AAudioStream_getFramesPerBurst

// libopenxr_loader.so: the dispatcher's XrHapticAmplitudeEnvelopeVibrationFB -> 100 Hz samples (x20 = the struct:
// +16 duration in ns, +24 amplitudeCount)
    .globl fake_envelope_samples
    .type fake_envelope_samples, %function
fake_envelope_samples:
    ldr w8, [x20]
    mov w9, #28105
    movk w9, #15261, lsl #16      // XR_TYPE_HAPTIC_AMPLITUDE_ENVELOPE_VIBRATION_FB
    cmp w8, w9
    b.ne 1f
    ldr w9, [x20, #24]
#ifdef FIXED
    mov x8, #225833675390976
    ldr d1, [x20, #16]
    movk x8, #16845, lsl #48      // 1e9
    ucvtf d0, w9
    fmov d2, x8
    scvtf d1, d1
    mov w8, #1120403456           // 100.0f
    fmul d0, d0, d2
    fmov s2, w8
    fdiv d0, d0, d1
    ucvtf s1, w9
    fmul s1, s1, s2
    fcvt s0, d0
    fdiv s0, s1, s0
#else
    ldr x8, [x20, #16]
    ucvtf s0, w9
    scvtf s1, x8
    mov w8, #1120403456
    fmov s2, w8
    fdiv s1, s0, s1
    fmul s0, s0, s2
    fdiv s0, s0, s1
#endif
    fcvtzu w21, s0
1:
    ret
    .size fake_envelope_samples, .-fake_envelope_samples

// libovrplatformloader.so
    .globl ovr_Microphone_GetOutputBufferMaxSize
    .type ovr_Microphone_GetOutputBufferMaxSize, %function
ovr_Microphone_GetOutputBufferMaxSize:
#ifdef FIXED
    stp x29, x30, [sp, #-32]!
    stp x20, x19, [sp, #16]
    mov x29, sp
    cbz x0, 3f
    ldr x8, [x0, #24]
    mov w19, #48000
    cbz x8, 2f
    mov x20, x0
    mov x0, x8
    bl AAudioStream_getFramesPerBurst
    cmp w0, w19
    b.hi 2f
    ldr x0, [x20, #24]
    bl AAudioStream_getFramesPerBurst
    sxtw x19, w0
2:
    mov x0, x19
    ldp x20, x19, [sp, #16]
    ldp x29, x30, [sp], #32
    ret
3:
    mov w19, #48000
    mov x0, x19
    ldp x20, x19, [sp, #16]
    ldp x29, x30, [sp], #32
    ret
#else
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    ldr x0, [x0, #0x18]
    bl AAudioStream_getFramesPerBurst
    sxtw x0, w0
    ldp x29, x30, [sp], #16
    ret
#endif
    .size ovr_Microphone_GetOutputBufferMaxSize, .-ovr_Microphone_GetOutputBufferMaxSize
