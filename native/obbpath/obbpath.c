// SPDX-License-Identifier: GPL-3.0-only
// FramePort: the mounted-OBB path from the allocator of the library that frees it (patch frame.obb_mount_emul,
// GitHub #308, GRID Legends).
//
// Codemasters' libandroid_utils.so returns the path of a mounted OBB as a std::string (libc++ __ndk1, returned
// through x8): AndroidUtils::getMountedOBBFullPath(const char *). The game library (libgrid.so) frees that string
// with its own operator delete, inlined: it reads an allocator header 16 bytes in front of the text. libgrid.so
// exports its own operator new/delete (a memory manager with that header), but libandroid_utils.so is loaded first
// (androidutils.NativeActivity's static block) and so allocates with libc++'s operator new. On a Quest that never
// matters: vold's mount point "/mnt/obb/obb:N" fits inside the string object (libc++ keeps up to 22 characters
// there), nothing is on the heap and nothing is freed. In Lepton FramePort mounts nothing: the OBB is unpacked into
// a folder with the OBB's own name, and that path ("/storage/emulated/0/Android/obb/<package>/DataSet0.obb") is
// longer, so the game freed a libc++ block with its own allocator and crashed (SIGSEGV in getOBBMountPoints()).
//
// FramePort renames the caller's import of that function to fp_obb_mounted_path (below; same string, shorter,
// NUL-padded) and adds this library to the caller's DT_NEEDED. A long result is copied into a block from the
// operator new the *caller's* own code binds to (dlsym on the caller's handle: the caller first, then its
// dependencies), so the caller's delete gets a block of its own allocator. The original block is left alone (a few
// dozen bytes per OBB, once at start): which operator new made it can't be told for sure (the callee's binding
// depends on load order), and freeing it with the wrong one is the crash this avoids. Short results (in the string
// object) pass untouched, and nothing in the game's code changes.
#include <android/log.h>
#include <dlfcn.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define TAG_LOG "FrameBridge"
#define EXPORT __attribute__((visibility("default")))
#define LOG(...) __android_log_print(ANDROID_LOG_INFO, TAG_LOG, __VA_ARGS__)

#define MOUNTED_PATH "_ZN12AndroidUtils21getMountedOBBFullPathEPKc"

// libc++'s std::string (standard layout, little endian). Long form: cap = allocation size | 1, size, data. Short
// form: byte 0 = size << 1 (bit 0 clear), the characters follow in place.
struct ndk_string {
    uint64_t cap;
    uint64_t size;
    char *data;
};

typedef struct ndk_string (*mounted_fn)(const char *);
typedef void *(*new_fn)(size_t);

// the original function (the caller's import is renamed; this library's own import keeps the name), weak: looked up
// with dlsym when the loader didn't bind it
extern struct ndk_string au_mounted_path(const char *) __asm__(MOUNTED_PATH) __attribute__((weak));

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static const void *cached_caller;  // module base caller_new belongs to
static new_fn caller_new;
static int logged;

static const void *module_base(const void *addr) {
    Dl_info info;
    return dladdr(addr, &info) ? info.dli_fbase : NULL;
}

// symbol `name` as library `addr`'s own code binds it (dlsym on its handle: the library first, then its dependencies)
static void *module_symbol(const void *addr, const char *name) {
    Dl_info info;
    if (!dladdr(addr, &info) || !info.dli_fname) return NULL;
    void *h = dlopen(info.dli_fname, RTLD_NOW | RTLD_NOLOAD);
    if (!h) {  // a path inside the APK ("base.apk!/lib/..."): try the library's own name
        const char *slash = strrchr(info.dli_fname, '/');
        h = dlopen(slash ? slash + 1 : info.dli_fname, RTLD_NOW | RTLD_NOLOAD);
    }
    if (!h) return NULL;
    void *sym = dlsym(h, name);
    dlclose(h);  // NOLOAD only took a reference
    return sym;
}

static mounted_fn real_function(void) {
    if (au_mounted_path) return au_mounted_path;
    return (mounted_fn)dlsym(RTLD_DEFAULT, MOUNTED_PATH);
}

EXPORT struct ndk_string fp_obb_mounted_path(const char *name) {
    const void *ret = __builtin_return_address(0);
    mounted_fn real = real_function();
    if (!real) {
        struct ndk_string empty = {0, 0, NULL};  // "" (short form, size 0)
        LOG("fp_obbpath: %s not found; no mounted path for %s", MOUNTED_PATH, name ? name : "(null)");
        return empty;
    }
    struct ndk_string s = real(name);
    if (!(s.cap & 1) || !s.data) return s;  // short: in the object, nothing on the heap

    pthread_mutex_lock(&lock);
    const void *base = module_base(ret);
    if (!caller_new || base != cached_caller) {
        caller_new = (new_fn)module_symbol(ret, "_Znwm");
        cached_caller = base;
    }
    new_fn alloc = caller_new;
    int first = !logged;
    logged = 1;
    pthread_mutex_unlock(&lock);

    if (!alloc) {
        if (first) LOG("fp_obbpath: the caller's operator new not found; mounted path %s left as is", s.data);
        return s;
    }
    size_t bytes = (size_t)(s.cap & ~(uint64_t)1);
    if (bytes < s.size + 1) bytes = s.size + 1;
    char *copy = alloc(bytes);
    if (!copy) return s;
    memcpy(copy, s.data, s.size);
    copy[s.size] = 0;
    s.data = copy;  // the callee's block stays (see the top of this file)
    if (first) {
        Dl_info info;
        const char *who = dladdr(ret, &info) && info.dli_fname ? info.dli_fname : "?";
        LOG("fp_obbpath: mounted OBB path %s (%zu characters) handed to %s from its own operator new",
            s.data, (size_t)s.size, who);
    }
    return s;
}
