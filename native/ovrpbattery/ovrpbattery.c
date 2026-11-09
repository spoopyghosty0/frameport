// SPDX-License-Identifier: GPL-3.0-only
// FramePort controller battery for OVRPlugin games (patch frame.ovrp_controller_battery, GitHub #572).
//
// OVRPlugin's controller state (ovrpControllerState4/5/6) carries each Touch controller's battery level in percent.
// OVRPort's OpenXR OVRPlugin has no source for it on the Frame and leaves it at 0, so games that show or check it
// warn about empty controllers all the time (In Death: Unchained, through Unreal's OculusTouch_*_BatteryPercentRemaining
// keys). This library reports 100 % for a connected Touch controller whose level reads 0; every other field, and any
// real level an OVRPlugin reports, stays as it is.
//
// Wiring (the same as native/langpack): this library is added to libOVRPlugin.so's DT_NEEDED and the plugin's own
// exports ovrp_GetControllerState4/5/6 are made STB_LOCAL in its .dynsym (elf.hide_exports), so every lookup of those
// names (Unity's P/Invoke, Unreal's dlsym on the plugin's handle, imports) reaches the functions below. The plugin's
// originals are found by reading its in-memory .dynsym, where the entries are still present.
#define _GNU_SOURCE  // dl_iterate_phdr in glibc (host tests)
#include <android/log.h>
#include <dlfcn.h>
#include <elf.h>
#include <link.h>
#include <stdint.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, "FrameBridge", __VA_ARGS__)

#define PLUGIN_BASENAME "libOVRPlugin.so"
// ovrpControllerState4/5/6 all start with: 4 uint32 masks, IndexTrigger[2], HandTrigger[2], Thumbstick[2] and
// Touchpad[2] (vector2f each), then BatteryPercentRemaining[2] (unsigned char: left, right) at byte 64.
#define BATTERY_OFFSET 64
#define CONTROLLER_LTOUCH 0x1u
#define CONTROLLER_RTOUCH 0x2u
#define REPORTED_PERCENT 100

typedef struct {
    const char *name;
    void *addr;
} find_t;

static uintptr_t fixptr(uintptr_t base, uintptr_t v) { return v < base ? base + v : v; }

static size_t count_syms(uintptr_t base, const ElfW(Dyn) *dyn) {
    const uint32_t *gnu = NULL, *sysv = NULL;
    for (const ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        if (d->d_tag == DT_GNU_HASH) gnu = (const uint32_t *)fixptr(base, d->d_un.d_ptr);
        if (d->d_tag == DT_HASH) sysv = (const uint32_t *)fixptr(base, d->d_un.d_ptr);
    }
    if (sysv) return sysv[1];
    if (!gnu) return 0;
    uint32_t nbuckets = gnu[0], symoffset = gnu[1], bloom_size = gnu[2];
    const uint32_t *buckets = gnu + 4 + bloom_size * (sizeof(ElfW(Addr)) / 4);
    const uint32_t *chains = buckets + nbuckets;
    uint32_t max = 0;
    for (uint32_t i = 0; i < nbuckets; i++)
        if (buckets[i] > max) max = buckets[i];
    if (max < symoffset) return symoffset;
    while (!(chains[max - symoffset] & 1)) max++;
    return (size_t)max + 1;
}

static int find_cb(struct dl_phdr_info *info, size_t size, void *data) {
    (void)size;
    find_t *f = (find_t *)data;
    const char *n = info->dlpi_name ? info->dlpi_name : "";
    const char *slash = strrchr(n, '/');
    if (strcmp(slash ? slash + 1 : n, PLUGIN_BASENAME) != 0) return 0;
    uintptr_t base = info->dlpi_addr;
    const ElfW(Dyn) *dyn = NULL;
    for (int i = 0; i < info->dlpi_phnum; i++)
        if (info->dlpi_phdr[i].p_type == PT_DYNAMIC) dyn = (const ElfW(Dyn) *)(base + info->dlpi_phdr[i].p_vaddr);
    if (!dyn) return 1;
    const ElfW(Sym) *symtab = NULL;
    const char *strtab = NULL;
    for (const ElfW(Dyn) *d = dyn; d->d_tag != DT_NULL; d++) {
        if (d->d_tag == DT_SYMTAB) symtab = (const ElfW(Sym) *)fixptr(base, d->d_un.d_ptr);
        if (d->d_tag == DT_STRTAB) strtab = (const char *)fixptr(base, d->d_un.d_ptr);
    }
    size_t n_syms = count_syms(base, dyn);
    if (!symtab || !strtab || !n_syms) return 1;
    for (size_t i = 1; i < n_syms; i++) {
        const ElfW(Sym) *s = &symtab[i];
        if (s->st_shndx == SHN_UNDEF || s->st_value == 0 || ELF64_ST_TYPE(s->st_info) != STT_FUNC) continue;
        if (strcmp(strtab + s->st_name, f->name) == 0) {
            f->addr = (void *)(base + s->st_value);
            break;
        }
    }
    return 1;
}

static void *original(const char *name) {
    find_t f = {name, NULL};
    dl_iterate_phdr(find_cb, &f);
    if (!f.addr) LOG("ovrp battery: %s not found in %s", name, PLUGIN_BASENAME);
    return f.addr;
}

static void fill_battery(unsigned int mask, void *state) {
    if (!state) return;
    unsigned int connected;
    memcpy(&connected, state, sizeof connected);
    connected &= mask;
    unsigned char *battery = (unsigned char *)state + BATTERY_OFFSET;
    int changed = 0;
    if ((connected & CONTROLLER_LTOUCH) && !battery[0]) { battery[0] = REPORTED_PERCENT; changed = 1; }
    if ((connected & CONTROLLER_RTOUCH) && !battery[1]) { battery[1] = REPORTED_PERCENT; changed = 1; }
    static int logged;
    if (changed && !logged) {
        logged = 1;
        LOG("ovrp battery: the controller state has no battery level: reporting %d %% (connected 0x%x)",
            REPORTED_PERCENT, connected);
    }
}

typedef int (*PFN_GetState)(unsigned int mask, void *state);

#define WRAP(N)                                                         \
    EXPORT int ovrp_GetControllerState##N(unsigned int mask, void *state) { \
        static PFN_GetState real;                                       \
        static int tried;                                               \
        if (!tried) {                                                   \
            real = (PFN_GetState)original("ovrp_GetControllerState" #N); \
            tried = 1;                                                  \
        }                                                               \
        if (!real) return -1000; /* ovrpFailure */                      \
        int result = real(mask, state);                                 \
        if (result >= 0) fill_battery(mask, state);                     \
        return result;                                                  \
    }

WRAP(4)
WRAP(5)
WRAP(6)
